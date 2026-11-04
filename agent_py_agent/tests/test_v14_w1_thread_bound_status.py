# LLM: thread变更是明确身份事实，不能把旧线程的单调高水位带到新线程。
# 模块用途: 守住状态DTO、同线程单调合并和旧回合晚到帧隔离。
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.model_metrics import public_model_metrics
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_runtime import (
    TuiRuntime,
    _next_background_activity_state,
    _TuiBackgroundActivityState,
)
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStatus, _status_with_update


# LLM: 使用生产公开schema构造指标，不将正文或任意字段当事实。
# 函数用途: 创建合法的上下文投影。
def _usage(tokens):
    return {'schema': 'model_visible_context_usage.v1', 'estimated': True,
            'context_window_tokens': 500000, 'compact_trigger_tokens': 300000,
            'current_tokens': tokens, 'messages_tokens': tokens}


# LLM: sampled_at_ns仍交原metrics协议选择，thread是独立边界。
# 函数用途: 创建旧线程高水位与新线程的小样本。
def _metrics(tokens, sampled=20):
    return public_model_metrics({'schema': 'model_runtime_metrics.v1', 'input_tokens': tokens,
            'model_rounds': 7, 'sampled_at_ns': sampled})


# LLM: 两层合并独立验证，防止只修runtime而reducer仍保旧数值。
# 函数用途: 跨线程必须采用空白新事实，而同线程仍保持单调。
def test_both_status_merges_replace_on_thread_change_including_empty_values():
    old = _TuiBackgroundActivityState(compact_count=14, context_usage=_usage(326895),
                                     model_metrics=_metrics(7000000))
    old = replace(old, thread_id='thread-old')
    empty = {'compact_count': 0, 'context_usage': {}, 'model_metrics': {}}
    new = _next_background_activity_state(old, 0, empty, thread_id='thread-new')
    assert new.thread_id == 'thread-new'
    assert new.compact_count == 0 and new.context_usage == {} and new.model_metrics == {}
    sequencer = TuiEventSequencer('status')
    status = replace(TuiStatus(), thread_id='thread-old')
    status = _status_with_update(status, sequencer.emit('status_updated', 'updated', 'status', {
        'thread_id': 'thread-old', 'compact_count': 14,
        'context_usage': _usage(326895), 'model_metrics': _metrics(7000000)}))
    assert status.context_tokens == 326895
    result = _status_with_update(status, sequencer.emit('status_updated', 'updated', 'status', {
        'thread_id': 'thread-new', **empty}))
    assert result.thread_id == 'thread-new' and result.compact_count == 0
    assert result.context_tokens == 0 and result.context_usage is None and result.model_metrics == {}
    same = _status_with_update(status, sequencer.emit('status_updated', 'updated', 'status', {
        'thread_id': 'thread-old', **empty}))
    assert same.compact_count == 14 and same.context_tokens == 326895
    assert same.model_metrics == status.model_metrics


# LLM: 没有thread字段的旧调用必须继续工作，不能为修新协议破坏已有调用方。
# 函数用途: 检查legacy空身份和同线程回退保护。
@pytest.mark.parametrize('thread_id', ['', 'thread-old'])
def test_legacy_and_same_thread_preserve_monotonic_merge(thread_id):
    old = replace(_TuiBackgroundActivityState(compact_count=14,
        context_usage=_usage(326895), model_metrics=_metrics(7000000)), thread_id='thread-old')
    stale = _next_background_activity_state(old, 0,
        {'compact_count': 0, 'context_usage': _usage(1), 'model_metrics': {}}, thread_id=thread_id)
    assert stale.thread_id == 'thread-old' and stale.compact_count == 14
    assert stale.context_usage == old.context_usage and stale.model_metrics == old.model_metrics


# LLM: 外部/new没有本地reset时，confirmed poll的thread也必须是指标唯一范围。
# 函数用途: 真实runtime跨线程清零并拒绝旧adapter的晚到指标。
def test_runtime_external_successor_clears_status_and_late_old_frames():
    runtime = TuiRuntime('external-successor-status')
    runtime.background_message_thread_id = 'thread-old'
    runtime._background_watch_gateway_binding = (runtime.session_id, 'thread-old')
    runtime.update_background_activity(0, {'thread_id': 'thread-old', 'compact_count': 14,
        'context_usage': _usage(326895), 'model_metrics': _metrics(7000000)})
    old_turn = runtime.begin_turn('old-turn')
    runtime.background_message_thread_id = 'thread-new'
    runtime._background_watch_gateway_binding = (runtime.session_id, 'thread-new')
    assert runtime.update_background_activity(0, {'thread_id': 'thread-new', 'compact_count': 0,
                                                'context_usage': {}, 'model_metrics': {}})
    status = runtime.store.snapshot().status
    assert status.thread_id == 'thread-new' and status.compact_count == 0
    assert status.context_tokens == 0 and status.context_usage is None and status.model_metrics == {}
    old_turn.write_context_usage(_usage(326895))
    old_turn.write_model_metrics(_metrics(7000000, 100))
    runtime.update_background_activity(0, {'thread_id': 'thread-old', 'compact_count': 14,
        'context_usage': _usage(326895), 'model_metrics': _metrics(7000000, 200)})
    assert runtime.store.snapshot().status == status
    current = runtime.begin_turn('new-turn')
    assert current.write_context_usage(_usage(1200))
    assert current.write_model_metrics(_metrics(1400, 1))
    result = runtime.store.snapshot().status
    assert result.thread_id == 'thread-new' and result.compact_count == 0
    assert result.context_tokens == 1200 and result.model_metrics['input_tokens'] == 1400


# LLM: reset是权威thread边界，晚到旧回合与缺字段心跳都不应复活旧高水位。
# 函数用途: 检查reset的thread元数据与首次新轮读数。
def test_reset_binds_thread_to_both_status_and_background_state():
    runtime = TuiRuntime('reset-status')
    runtime.update_background_activity(0, {'compact_count': 14, 'context_usage': _usage(326895),
                                         'model_metrics': _metrics(7000000)})
    old = runtime.begin_turn('old')
    runtime.publish_conversation_reset('thread-fresh')
    assert runtime.store.snapshot().status.thread_id == 'thread-fresh'
    assert runtime._background_activity._state.thread_id == 'thread-fresh'
    before = runtime.store.snapshot().status
    old.write_context_usage(_usage(326895))
    old.write_model_metrics(_metrics(7000000, 100))
    assert runtime.store.snapshot().status == before
    runtime.update_background_activity(0, {})
    new = runtime.begin_turn('new')
    new.write_context_usage(_usage(2500))
    assert runtime.store.snapshot().status.thread_id == 'thread-fresh'
    assert runtime.store.snapshot().status.compact_count == 0
    assert runtime.store.snapshot().status.context_tokens == 2500


# LLM: 指标基线可跨线程清空，但已有活动块不能再次started而被reducer拒绝。
# 函数用途: 检查visible→visible首帧和相同心跳均不留旧数值。
def test_review_visible_successor_activity_replaces_old_block_immediately():
    runtime = TuiRuntime('visible-successor')
    runtime.background_message_thread_id = 'old'
    assert runtime.update_background_activity(1, {'thread_id': 'old', 'compact_count': 14,
        'context_usage': _usage(326895), 'model_metrics': _metrics(7000000)})
    runtime.background_message_thread_id = 'new'
    incoming = {'thread_id': 'new', 'compact_count': 0, 'context_usage': {}, 'model_metrics': {}}
    assert runtime.update_background_activity(1, incoming)
    status = runtime.store.snapshot().status
    assert status.thread_id == 'new' and status.compact_count == 0 and status.context_tokens == 0
    assert not status.model_metrics
    runtime.update_background_activity(1, incoming)
    assert runtime.store.snapshot().status == status


# LLM: 新Context先于首次activity心跳到达，不能给旧缓存仅换标签而保留高水位。
# 函数用途: 检查cursor已重基但状态与后台缓存尚未同步的窗口。
@pytest.mark.parametrize('compact_generation', [None, 0])
def test_review_context_before_successor_activity_has_fresh_merge_baselines(compact_generation):
    runtime = TuiRuntime('context-first-successor')
    runtime.background_message_thread_id = 'old'
    runtime.update_background_activity(0, {'thread_id': 'old', 'compact_count': 14,
        'context_usage': _usage(326895), 'model_metrics': _metrics(7000000)})
    assert runtime._background_activity._state.compact_count == 14
    runtime.background_message_thread_id = 'new'
    turn = runtime.begin_turn('new-request')
    usage = _usage(1200)
    if compact_generation is not None:
        usage['compact_generation'] = compact_generation
    assert turn.write_context_usage(usage)
    assert runtime._background_activity._state.compact_count == 0
    assert not runtime._background_activity._state.model_metrics
    assert runtime.store.snapshot().status.compact_count == 0
    runtime.update_background_activity(0, {'thread_id': 'new', 'compact_count': 0, 'model_metrics': {}})
    status = runtime.store.snapshot().status
    assert status.thread_id == 'new' and status.compact_count == 0
    assert status.context_tokens == 1200 and not status.model_metrics
