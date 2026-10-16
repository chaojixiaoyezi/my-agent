# LLM: 核对真实恢复入口的同时刻投影和覆盖，只有HTTP返回替身，不读真实会话或凭据。
# 模块用途: 禁止transcript在受保护最新事实前跳项再拼活动工具来源。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as tool_service
from agent_py_agent.agent.agent_core import compact_request_recovery as recovery_core
from agent_py_agent.agent.agent_core import compact_transcript_prefix as prefix_core
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import CompactionSummary, RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation import background_execution, compact
from agent_py_agent.agent.conversation import compact_request_budget as budgets
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
from agent_py_agent.tests.test_compact_request_budget import _request
from agent_py_agent.tests.test_mixed_compact_recovery import _mixed_background, _wire_from_material
from agent_py_agent.tests.test_subagent_compact_recovery import _http


# LLM: 冻结主前缀不走存档非文本检查；超预算只报容量失败，不偷偷改写或分段媒体。
# 函数用途: 锁住媒体前缀超预算与旧存档来源非文本的不同错误码。
def test_frozen_media_prefix_budget_failure_is_not_archive_nontext(monkeypatch):
    from agent_py_agent.tests.test_media_compact_preflight import IMAGE

    original = [{"role": "user", "content": [IMAGE, {"type": "text", "text": "完整正文" * 20_000}]}]
    source = CompactMessageSource(lambda: iter(original), preserve_prefix=True)
    monkeypatch.setattr(budgets, "generate_auxiliary_model_response", lambda *_: pytest.fail("超预算不得发送改写/分段请求"))
    with pytest.raises(ConversationCompactError) as failure:
        budgets.generate_bounded_compact_response(replace(_request("摘要"), messages=None), message_source=source)
    assert failure.value.code == "COMPACT_PREFIX_TOO_LARGE"
    assert list(source) == original and original[0]["content"][0] == IMAGE


def _blocks(wire):
    # Anthropic缓存锚点随新尾项推进；正文块与顺序仍逐项精确比较。
    return [(message['role'], {k: v for k, v in block.items() if k != 'cache_control'}) for message in wire['messages']
            for block in ([{'type': 'text', 'text': message['content']}] if isinstance(message['content'], str)
                          else message['content'])]


# LLM: 旁听解绑前的同次冻结来源，纯投影到真实provider布局；不重跑准备，也不替换摘要选源。
# 函数用途: 保存真正准备摘要时的主投影，不能用先前溢出的HTTP代替同一时刻证据。
def _capture_frozen_wires(monkeypatch, agent):
    from copy import deepcopy

    captured = []
    original = recovery_core._transcript_summary_surface

    # LLM: 只读同次冻结值，纯builder投影后交回原选源函数。
    # 函数用途: 捕获与摘要同一时刻的主请求布局。
    def surface(recovery, frozen, tools):
        projection = recovery_core.project_tool_loop_request(frozen)
        captured.append(deepcopy(_wire_from_material(
            agent, SimpleNamespace(projection=projection, request_input=frozen),
        )))
        return original(recovery, frozen, tools)

    monkeypatch.setattr(recovery_core, '_transcript_summary_surface', surface)
    return captured


@pytest.mark.parametrize('backend', ['anthropic_compatible', 'openai_compatible'])
# LLM: 原业务溢出后同时核实际摘要HTTP和真正冻结主投影，合法工具覆盖及保护事实仍保留。
# 函数用途: 验证合法联合摘要的同次连续前缀。
def test_transcript_stops_before_latest_facts_with_eligible_joint_tools(tmp_path, monkeypatch, backend):
    agent, store, thread, request, execution, sink, _ = _mixed_background(
        tmp_path, backend=backend, detached=False,
    )
    main, summary = [], []
    original = compact._summarize
    frozen_wires = _capture_frozen_wires(monkeypatch, agent)
    summarizing = False

    def summarize(*args, **kwargs):
        nonlocal summarizing
        summarizing = True
        try:
            return original(*args, **kwargs)
        finally:
            summarizing = False

    monkeypatch.setattr(compact, '_summarize', summarize)

    def receive(wire, _number):
        (summary if summarizing else main).append(wire)
        if not summarizing and len(main) == 1:
            raise ProviderContextWindowError('前缀回归溢出')

    _http(monkeypatch, backend=backend, on_business=receive)
    background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt='继续联合核对', continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )
    assert len(summary) == 1
    source = _blocks(summary[0])[:-1]
    assert len(frozen_wires) == 1
    assert source == _blocks(frozen_wires[0])[:len(source)]
    assert source and source == _blocks(main[0])[:len(source)]
    checkpoint = committed_compact_checkpoint_chain(agent, store.threads.require(thread.thread_id))[-1]
    assert len(checkpoint['source_tool_refs']) == 2
    assert 'MIXED_SOURCE_MARKER' in str(summary[0])
    assert len(source) < len(_blocks(main[0]))


@pytest.mark.parametrize('protected_before_tools', [False, True])
def test_joint_boundary_never_crosses_protected_facts(protected_before_tools):
    user = UserTurn(text='保留用户要求')
    from agent_py_agent.agent.conversation.tool_context_window import native_carried_tool_handoff
    records = ({'call_id': 'a'},)
    context = ['完整工具原文']
    tools = CompactionSummary(native_carried_tool_handoff(context, records), source='carried_tool_handoff')
    facts = RuntimeFactsTurn(text='必须保留的最新事实', source='conversation.runtime')
    frozen = SimpleNamespace(tool_ir_history=(user, facts, tools) if protected_before_tools else (user, tools, facts),
                             tool_context=tuple(context), provider_history_messages=({}, {}))
    source = SimpleNamespace(source_ir_history=(), source_records=records)
    end = prefix_core._joint_prefix_end(
        frozen, source, prefix_core.TranscriptPrefixSelection(SimpleNamespace(archive_tool_calls=records)),
    )
    assert end == (0 if protected_before_tools else 4)


@pytest.mark.parametrize('backend', ['anthropic_compatible', 'openai_compatible'])
# LLM: 在模型安全点提供保护事实位于工具之前的合法IR，主请求与冻结都读同一参数；两次HTTP和checkpoint守同一边界。
# 函数用途: 证明不能跳过中间保护事实再覆盖后面的工具，工具原文仍在恢复请求中。
def test_real_recovery_keeps_tools_after_protected_middle_fact(tmp_path, monkeypatch, backend):
    agent, store, thread, request, execution, sink, _ = _mixed_background(
        tmp_path, backend=backend, detached=False,
    )
    prior_ids = [row.message_id for row in store.messages.recent(thread.thread_id, limit=0)]
    original_next = tool_service.next_tool_loop_model_response

    # LLM: 仅测试夹具插入受保护IR；主发送和冻结都继续使用同一个真实参数。
    # 函数用途: 构造工具前的保护中段，不能只修改摘要投影。
    def next_response(agent, params, tool_rounds):
        if not any(isinstance(item, RuntimeFactsTurn) and item.text == 'PROTECTED_MIDDLE_FACT'
                   for item in params.tool_ir_history):
            params.tool_ir_history.insert(0, RuntimeFactsTurn(
                text='PROTECTED_MIDDLE_FACT', source='conversation.runtime',
            ))
        return original_next(agent, params, tool_rounds)

    monkeypatch.setattr(tool_service, 'next_tool_loop_model_response', next_response)
    main, summary = [], []
    original_summary = compact._summarize
    frozen_wires = _capture_frozen_wires(monkeypatch, agent)
    summarizing = False

    # LLM: 标记真实摘要发送阶段，始终执行原函数并用finally恢复标记。
    # 函数用途: 区分保护中段用例的摘要HTTP和业务HTTP。
    def summarize(*args, **kwargs):
        nonlocal summarizing
        summarizing = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            summarizing = False

    monkeypatch.setattr(compact, '_summarize', summarize)

    # LLM: 只替换末端HTTP；首次业务typed拒绝后仍走原恢复和摘要。
    # 函数用途: 保存两个阶段的实际出站负载并触发一次恢复。
    def receive(wire, _number):
        (summary if summarizing else main).append(wire)
        if not summarizing and len(main) == 1:
            raise ProviderContextWindowError('保护中段边界恢复')

    _http(monkeypatch, backend=backend, on_business=receive)
    background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt='继续联合核对', continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )
    assert len(summary) == 1 and len(main) == 2
    source = _blocks(summary[0])[:-1]
    assert len(frozen_wires) == 1
    assert source == _blocks(frozen_wires[0])[:len(source)]
    assert source and source == _blocks(main[0])[:len(source)]
    assert 'PROTECTED_MIDDLE_FACT' not in str(summary[0])
    assert 'MIXED_SOURCE_MARKER' not in str(summary[0])
    assert 'PROTECTED_MIDDLE_FACT' in str(main[0])
    assert 'MIXED_SOURCE_MARKER' in str(main[0]) and 'MIXED_SOURCE_MARKER' in str(main[-1])
    checkpoint = committed_compact_checkpoint_chain(agent, store.threads.require(thread.thread_id))[-1]
    assert checkpoint['source_message_ids'] == prior_ids
    assert checkpoint['source_tool_refs'] == []


@pytest.mark.parametrize('overflow', [False, True])
def test_frozen_prefix_never_shrinks_or_segments(monkeypatch, overflow):
    request = _request('短运行状态' if overflow else '原文' * 10000)
    original = request.messages
    request = replace(request, messages=None)
    source = CompactMessageSource(lambda: iter(original), preserve_prefix=True)
    calls = []

    def generate(r):
        calls.append(r)
        raise ProviderContextWindowError('真实typed窗口拒绝')

    monkeypatch.setattr(budgets, 'generate_auxiliary_model_response', generate)
    monkeypatch.setattr(budgets, 'shrink_tool_outputs', lambda *_args: pytest.fail('不能改写主前缀'))
    monkeypatch.setattr(budgets, '_summarize_segments', lambda *_args: pytest.fail('不能另造分段请求'))
    with pytest.raises(ConversationCompactError) as error:
        budgets.generate_bounded_compact_response(request, message_source=source)
    assert error.value.code == 'COMPACT_PREFIX_TOO_LARGE'
    assert len(calls) == int(overflow)
    assert list(source) == original


def test_prefix_rejects_skipped_coverage_ids():
    rows = tuple(SimpleNamespace(message_id=str(i), role='user', content=f'短事件{i}', metadata={}) for i in range(3))
    projected = prefix_core.provider_history_messages_from_rows(rows)
    prefix = prefix_core.TranscriptInputPrefix(projected, 0, rows, ('0', '1', '2'))
    assert list(prefix(rows)) == list(projected)
    with pytest.raises(ConversationCompactError, match='覆盖不是主投影前缀'):
        prefix((rows[0], rows[2]))


@pytest.mark.parametrize('case', ['partial_history', 'foreign_handoff', 'unknown_ir', 'projection_mismatch'])
def test_joint_rejects_unproven_input_coverage(monkeypatch, case):
    rows = tuple(SimpleNamespace(message_id=str(i), role='user', content=f'事件{i}', metadata={}) for i in range(2))
    history = prefix_core.provider_history_messages_from_rows(rows)
    handoff = CompactionSummary('其他记录Q的交接，不含R', source='carried_tool_handoff')
    frozen = SimpleNamespace(provider_history_messages=history, tool_ir_history=(handoff,))
    source = SimpleNamespace(messages=rows[:1] if case == 'partial_history' else rows)
    tools = SimpleNamespace(source_ir_history=(handoff,) if case != 'foreign_handoff' else (),
                            source_records=({'call_id': 'R'},) if case == 'foreign_handoff' else ())
    if case == 'unknown_ir':
        frozen.tool_ir_history = (CompactionSummary('不允许覆盖的未知U', source='unknown'), handoff)
    tail = prefix_core.AnthropicMessageAdapter().to_provider_messages(frozen.tool_ir_history)
    if case == 'projection_mismatch':
        tail = [{'role': 'user', 'content': '另一个投影，边界不能凭长度猜'}]
    monkeypatch.setattr(prefix_core, 'project_tool_loop_request', lambda _f: SimpleNamespace(
        status='ready', messages=list(history) + tail))
    params = SimpleNamespace(conversation_history_seed=SimpleNamespace(source=SimpleNamespace(projected_rows=lambda: rows)),
                             archive_tool_calls=[])
    captured = prefix_core.capture_transcript_prefix(frozen, source, prefix_core.TranscriptPrefixSelection(params, tools))
    assert captured.joint_end == 0
    assert list(captured(source.messages)) == list(history[:len(source.messages)])


@pytest.mark.parametrize('backend', ['anthropic_compatible', 'openai_compatible'])
# LLM: 只在测试安全点加入 typed 宿主附件；两个图均导入显式临时 owner home，HTTP 仍是末端替身。
# 函数用途: 旧历史图进入冻结摘要，当前宿主图留在原 IR 并随恢复业务继续发送。
def test_historical_and_current_host_images_keep_distinct_compact_boundaries(tmp_path, monkeypatch, backend):
    import json

    from agent_py_agent.agent.conversation.input_media import (
        import_input_media,
        input_media_blocks,
        input_media_root,
    )
    from agent_py_agent.agent.conversation.native_history import (
        CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
        canonical_native_messages_envelope,
    )
    from agent_py_agent.tests.test_media_compact_preflight import PNG

    agent, store, thread, request, execution, sink, _ = _mixed_background(tmp_path, backend=backend, detached=False)
    assert agent.home_paths.root.resolve() == (tmp_path / 'home').resolve()
    old_path, new_path = tmp_path / 'old.png', tmp_path / 'new.png'
    old_path.write_bytes(PNG)
    new_path.write_bytes(PNG + b'\n')
    old = import_input_media(old_path, input_media_root(agent))
    current = import_input_media(new_path, input_media_root(agent))
    assert old['sha256'] != current['sha256']
    native = [{'role': 'user', 'content': [{'type': 'text', 'text': 'OLD_HISTORY_IMAGE'}, *input_media_blocks([old])]},
              {'role': 'assistant', 'content': [{'type': 'text', 'text': '旧图已确认'}]}]
    stored = store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '旧图已确认',
        'metadata': {'conversation_request_id': 'completed-image',
            CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(native)}})
    original_next = tool_service.next_tool_loop_model_response

    def next_response(host, params, tool_rounds):
        if not any(isinstance(item, RuntimeFactsTurn) and item.source == 'fixture.current_host_media'
                   for item in params.tool_ir_history):
            params.tool_ir_history.insert(0, RuntimeFactsTurn('CURRENT_HOST_IMAGE',
                source='fixture.current_host_media', media=(current,)))
        return original_next(host, params, tool_rounds)

    monkeypatch.setattr(tool_service, 'next_tool_loop_model_response', next_response)
    main, summaries = [], []
    original_summary = compact._summarize
    summarizing = False

    def summarize(*args, **kwargs):
        nonlocal summarizing
        summarizing = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            summarizing = False

    monkeypatch.setattr(compact, '_summarize', summarize)

    def receive(wire, _number):
        (summaries if summarizing else main).append(wire)
        if not summarizing and len(main) == 1:
            raise ProviderContextWindowError('历史与当前宿主图的恢复边界')

    _http(monkeypatch, backend=backend, on_business=receive)
    background_execution.run_background_turn_with_compact(execution, thread, request,
        user_prompt='比较旧图和当前图', continuation_injection=[], proactive_delivery_available=False, activity_sink=sink)
    assert len(main) == 2 and len(summaries) == 1
    summary = json.dumps(summaries[0], ensure_ascii=False)
    assert 'OLD_HISTORY_IMAGE' in summary and 'CURRENT_HOST_IMAGE' not in summary
    assert all('CURRENT_HOST_IMAGE' in json.dumps(wire, ensure_ascii=False) for wire in main)

    def images(value):
        if isinstance(value, dict):
            return int(value.get('type') in ('image', 'image_url')) + sum(images(item) for item in value.values())
        return sum(images(item) for item in value) if isinstance(value, list) else 0

    assert images(main[0]) == 2 and images(summaries[0]) == 1 and images(main[-1]) == 1
    checkpoint = committed_compact_checkpoint_chain(agent, store.threads.require(thread.thread_id))[-1]
    assert checkpoint['media_blocks_archived'] == 1 and checkpoint['media_refs'] == [old['sha256']]
    assert stored.message_id in checkpoint['source_message_ids']
    assert stored in store.messages.recent(thread.thread_id, limit=0)


# LLM: 纯容量恢复只改变显式模型窗口，不改源消息、权限、媒体或任何 checkpoint；模型调用是本地替身。
# 函数用途: 证明冻结前缀容量失败后仍可在足够窗口中完整重试，失败没有破坏来源。
def test_prefix_capacity_failure_can_retry_same_source_with_larger_window(monkeypatch):
    from agent_py_agent.agent.backends.base import ModelResponse

    request = _request('完整历史材料' * 4000)
    original = request.messages
    source = CompactMessageSource(lambda: iter(original), preserve_prefix=True)
    request = replace(request, messages=None)
    sent = []
    monkeypatch.setattr(budgets, 'generate_auxiliary_model_response',
        lambda candidate: sent.append(candidate) or ModelResponse(text='保留完整来源的摘要', backend='fake'))
    with pytest.raises(ConversationCompactError) as error:
        budgets.generate_bounded_compact_response(request, message_source=source)
    assert error.value.code == 'COMPACT_PREFIX_TOO_LARGE' and not sent
    request.agent.config.model_context_window_tokens = 200_000
    result = budgets.generate_bounded_compact_response(request, message_source=source)
    assert result.text == '保留完整来源的摘要' and len(sent) == 1
    assert sent[0].messages == original and list(source) == original
